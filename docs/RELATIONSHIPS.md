# People: one person, every channel

Status: M5 "People" (architecture 4.7, 7.4). Implementation in this release.

Protagine keeps a persistent social memory for an agent: who it talks to,
what standing each relationship has, whether it may reach out to them and
how often. This document is the durable spec for the identity and contact
layer. It was first written against an audit of a deployment where the
machinery existed but attribution failed (95%+ of non-owner traffic landed on
a `default` pseudo-contact and third parties accumulated zero history).

## Design principles

1. **Attribution before analysis.** Every downstream capability (affect,
   facts, cadence, outreach, the digest) is only as good as knowing WHO each
   utterance came from. Per-message sender resolution is the foundation.
2. **A person is one contact with many handles.** The unit of identity is
   the contact; channels contribute handles (`whatsapp`, `sms`, `email`,
   `voice`, any custom gateway, ...). Matching is deterministic where safe
   (an exact handle, an E.164 number on any gateway, a normalized email),
   and a PROPOSAL where fuzzy (the same display name in a shared group
   scope), which the owner confirms. Silent fuzzy merges are forbidden: a
   wrong merge poisons two histories at once.
3. **Meet people by default.** A sender that cannot be matched gets a shadow
   contact (tier `unknown`, `may_contact='ask'`, provenance recorded) so
   history accrues from first contact. There is no switch: shadows are the
   design. A shadow is remembered, but the social drive ignores it until the
   owner sets a cadence or a tier, so a group chat never becomes a stream of
   check-in asks.
4. **A tier is standing, never permission.** Whether the agent may reach out
   is `may_contact` (never, ask, auto), raised only by the owner and lowered
   by a contact's opt-out. Familiarity, affect and relationship estimates
   never change it.
5. **Machines are not people.** Turns of system origin (cron prompts,
   skill invocations, self-echo) never write affect, facts or interactions.
   They attribute to the reserved `system` sentinel and are excluded from
   every relationship surface.
6. **Stores accept only real contacts.** The ToM APIs validate contact ids
   against the contact store; test strings and free-text names are refused
   rather than silently minting state.

## Architecture

Turn context shows the contact's recorded interaction count, last interaction
date and stored contact tier separately. It does not translate the legacy
`relationship_score` into a closeness label or percentage: that heuristic blends
contact mood with interaction frequency and recency, which do not establish the
agent's attachment. The stored score remains available for existing consumers.
Contact affect describes the contact; agent judgments have their own sourced
representation. These observations do not grant tool authority.

### 1. Sender flows with every turn

`TurnSyncRequest` gains an optional `sender`:

```json
"sender": {
  "platform": "whatsapp",           // channel kind
  "user_id": "1234567@lid",         // per-platform sender identifier
  "display_name": "Sam Rivera",   // best-effort, for shadow naming + fuzzy proposals
  "group_id": "5551212-16999@g.us"  // set when the turn came from a group
}
```

Hosts that cannot supply it lose nothing (legacy behavior); hosts that can
(the Hermes provider passes what `pre_llm_call` already carries) get
authoritative server-side attribution regardless of client caching bugs.

### 2. ParticipantResolver (sidecar, `identity/participants.py`)

`resolve(platform, user_id, display_name, group_id, channel_id) -> Resolution(contact_id, method, created)`

Resolution ladder, first hit wins:
1. **Exact transport handle**: `contact_handles(gateway=platform, address=user_id)`
   in its stored form, a verified one first, then any other that is not a
   name guess. An explicit per-channel correction, or a second contact kept
   on the same number as a separate alias, takes precedence over
   cross-gateway phone inference.
2. **Canonical identity (C1)**: `canonical_handle(gateway, address)` in
   `contacts/store.py`. An address that parses as an E.164 number
   (`is_e164`: `^\+?[1-9]\d{6,14}$` after stripping spaces, dashes, dots and
   parentheses; the numeric local part of a `number@host` id counts; a bare
   10-digit number is NANP) is the phone identity on **any** gateway and
   matches on `phone_key` whatever gateway stored it. Nothing names a channel:
   a phone channel nobody listed resolves like `sms`. An email matches on its
   lower-cased form; anything else matches only its exact gateway and
   address. Two contacts sharing one number is ambiguous and resolves to
   none, never a guess.
3. **Scoped display-name (PROPOSAL only)**: `display_name` uniquely matches
   one member of the same group scope. A candidate association is filed and
   the sender keeps a separate shadow identity. The candidate becomes an
   owner ask (`link_proposal`); `store.confirm_link` attaches the handle
   (folding the shadow into the person through `merge`) and
   `store.reject_link` closes it.
4. **Shadow contact**: tier `unknown`, `may_contact='ask'`,
   `import_source=auto:sender`, the handle attached. Stored handles keep the
   transport gateway they arrived on (it is needed to send).

The resolver also OWNS the machine gate: senderless turns on machine
channels (`cron:`, `api:` prefixes, `PROTAGINE_IDENTITY_MACHINE_CHANNELS`) or
system-origin text resolve to the `system` sentinel.

### Current source attribution in active requests

An owner correction can move one exact handle and explicitly selected sources
without rewriting their original bytes. It invalidates source-linked descendant
answers and their projections. Reversing the correction restores direct-source
attribution; it does not revive those old derived conclusions or grant authority.

The Hermes request middleware checks cited source ownership, version and session
scope alongside the existing erasure feed. Its POST form at
`/memory/sources/erasures` accepts up to 512 exact source references and returns
`sources_current`, under the same `turns:write` scope as GET. The existing 250 ms
request budget and bounded feed pagination remain. No new queue, database or
service is involved. Changed attribution, unavailable validation, or an oversized
reference set withholds current recalled evidence and opened-source receipts.
Current human input stays intact. Deploy the backend before the updated plugin.

This checks structured native recall and authenticated source-read receipts.
It does not identify copied paraphrases or retroactively relabel native transcript
storage. An unchanged content digest does not prove that the source still belongs
to the same person. The native social-tool fixture qualifies correction and
reversal during an active request, channel separation, and inherited-child tool
revocation without an external model or a message send.

### 3. turns/sync is the attribution chokepoint

On every synced turn:
- If `sender` is present, the resolver decides the contact (overriding the
  client-supplied `context.contact_id`, which remains the fallback).
- The contact's own words are checked for an opt-out (section 7).
- The resolved contact gets `record_interaction` (non-owner and owner alike;
  `system` never). It counts **conversations** (C3): a turn more than 30
  minutes after the contact's previous one starts a new conversation, a
  turn inside that window belongs to the current one, and
  `last_interaction_at` only moves forward.
- The comms ledger row carries the real `channel_id` (group, DM or voice).
- Affect and facts extraction runs against the resolved contact; `system`
  turns skip it entirely.

### 4. ToM boundary validation

`POST /affect/events` and `POST /mind/facts`: the contact must exist in the
store (or be the owner). Unknown ids are rejected with a clear error.
(`system` is storable in comms for ops visibility but refused by ToM.)

### 5. Voice and in-person

Voice is just another channel: the deployment's speaker-identity service
resolves a voice to a canonical contact and the gateway syncs the turn with
`sender={platform:"voice", user_id:<contact_id or enrolled-name>}`. A
`voice` handle kind links enrolled voiceprint names to contacts so kiosk
and call interactions accrue to the same person as their texts. Same for
`face` if a deployment enrolls faces. Protagine stays generic: it defines the
handle kinds; deployments supply the recognizers.

### 6. The owner's interfaces

One interface, three doors: the router `/v1/mind/people`
(`api/routers/people.py`), the tool `protagine_people` and the CLI
`protagine people`. `<who>` is a contact id, a phone number, an email,
`gateway:address` or a unique name (`store.resolve_reference`: a handle
address only one contact holds comes before any name, and an ambiguous name
resolves to nobody; with `exact=True` names are refused, which is how an
owner's grant to message a third party is kept to someone the owner
identified).

| Router | Tool | CLI | Who |
|---|---|---|---|
| `GET /?q=` | `who` | `people who [q]` | everyone (a guest sees id, name and tier) |
| `GET /{who}` | `inspect` | `people inspect <who>` | everyone (record, digest, handles, proposals and permission history for the owner) |
| `POST /link` | `propose_link` | `people link <who> <gateway> <address>` | everyone: a candidate the owner confirms |
| `GET /proposals` | | `people proposals` | the open candidates |
| `POST /{who}/permission` | `set_permission` | `people permit <who> never\|ask\|auto` | owner |
| `POST /{who}/cadence` | `set_cadence` | `people cadence <who> <minutes\|off>` | owner |
| `POST /merge` | `merge` | `people merge <keep> <drop>` | owner |

A mutation needs the caller's `contact_id` to be the owner, or `by: cli`
from the local CLI (the API key is the local owner); anything else is 403
`not_owner`. The tool refuses the three mutations outside the owner's own
interactive session before asking the sidecar.

**Merge (C2).** `store.merge(keep, drop, *, performed_by, reattribute, sources_of)`:
- every handle of `drop` moves through `identity_links.correct` (one durable
  receipt per handle, `merge:<drop>:<handle_id>`)
- the sources `drop` holds in the ledger ride on receipts of their own
  (`identity_links.move_sources`, `merge:<drop>:sources:<n>`, at most 100
  each, every source on exactly one receipt); the host's existing
  reconciliation of `pending_identity_reconciliations` re-attributes them,
  immediately for a merge through the router and otherwise on the source
  worker's next pass
- the other stores keyed by contact re-attribute through the
  `reattribute(old_id, new_id)` hooks (the comms log and the affect store)
- in one commit: `last_interaction_at` is the later of the two,
  `first_seen_at` the earlier, `interaction_count` the sum, the keeper's
  cadence else the dropped one's, `may_contact` `never` if either was (an
  opt-out survives a merge), the keeper's tier, the digests joined, tags
  and notes appended, identity candidates moved, `drop` soft-deleted
- both records are audited (`merged_in`, `merged_into`). A merge that stopped
  half way can simply be run again.

### 7. Outbound permission: `may_contact`

`contacts.may_contact` is `never`, `ask` or `auto` (architecture 7.4). The
owner is `auto` by identity. A new shadow, an introduction, a provisioned
handle and a migrated contact start at `ask`; a contact whose old
`interaction_allowed` was false migrated to `never` (migration
`006_may_contact.sql`, which drops `interaction_allowed`; the store needs
SQLite >= 3.35 and says so at connect).

- `store.set_may_contact(contact_id, value, *, by, reason)` is the owner's
  path, in any direction, audited `may_contact_set`.
- `store.lower_may_contact(contact_id, *, reason, source_ref)` goes to
  `never` only, audited `opt_out`. Two detectors end there: the phrase match
  in `contacts/optout.py` on the contact's own words (a bare `STOP`,
  `unsubscribe`, "don't message/text/contact me", "no more messages from
  you", "stop the check-ins", "I'd rather you didn't message me", "leave me
  alone", "remove me" and close variants; never for the owner) and the
  appraisal call's `opt_out` flag.
- Nothing else writes the column: `update()` refuses it, a tier promotion
  leaves it alone, and learning never touches it.

### 8. Cadence and the social drive's candidates

`contacts.cadence_minutes` is the owner's check-in cadence (`set_cadence`,
audited `cadence_set`; null clears it). `compute_cadence_overdue` honours it
exactly when set and otherwise estimates the contact's rhythm from their
conversations (active span / conversations), so a chatty contact no longer
collapses to the floor. `store.social_candidates()` lists what the social
drive may consider: `may_contact` is not `never` and the owner set a cadence
or the tier is `regular` or above. Shadow and group-only contacts are never
listed.

### 9. Per-contact digest

`contacts.digest` (with `digest_sources`) is a short template record of a
person: who they are, how they are reachable, when you last talked, what is
open and the top claims from their own sources. It never carries what the
owner set for them (permission, cadence, who introduced them): the digest is
shown as "About this person" when that person is the viewer and composes the
messages sent to them, so those stay in the owner's `inspect`, read from the
columns. The mind writes it daily for contacts with a conversation in the last
day (`store.set_digest`, sources `["template"]`). With the people faculty off
there is no digest section. It replaces the old relationship briefs.

### 10. Remediation of poisoned history (deployment runbook)

- `default`: stop new person-writes (machine gate); exclude it from every
  relationship surface; keep rows for ops history. No deletion (part of it
  is genuine pre-fix owner traffic).
- Test residue (`validate-*` and other non-cid ids): purge from the affect
  and facts stores; the ToM boundary validation prevents recurrence.

## Config

| Env | Default | Meaning |
|---|---|---|
| `PROTAGINE_IDENTITY_MACHINE_CHANNELS` | `cron,api,internal` | Channel prefixes whose senderless turns are `system` |

## Test plan

- `test_people_store.py`: migration 006 on a pre-006 store; `may_contact`
  moves only through the owner path and lowers only to `never`; cadence and
  digest; conversations (C3); `social_candidates`; `resolve_reference`; the
  C1 probe (three phone senders on `sms`, `whatsapp` and a custom gateway
  are three contacts on every later turn, no orphans; one number on two
  gateways is one contact; a non-E.164 custom id stays gateway-scoped);
  merge (C2) folds, moves handles, candidates and sources exactly once,
  calls the hooks and can run again after stopping half way; link proposals.
- `test_people_router.py`: every route; the owner check (403 for anyone
  else, accepted for the owner and `by: cli`); a guest sees only who someone
  is; a merge re-attributes ledger sources; evals 7.2 test 4.
- `test_optout.py`: the family's phrasings, close variants and near misses.
- `test_people_cli.py`, `tests/hermes_adapter/test_tools_commands.py` and
  `test_guard.py`: the CLI and the tool, owner-only mutations refused in a
  guest, worker and cron session, cron delivery to a `never` contact blocked.
- Resolver ladder: `test_resolve_messaging_handle.py`,
  `test_identity_corrections.py`, `test_signals_attribution.py`.

## Source erasure

Conversation-derived affect observations retain the canonical turn ID,
session and exact message hashes already used by shared facts. The existing
source-forget endpoint deletes their linked observations and recomputes
current state. Reads also reconcile erased evidence, including after restart
or an interrupted cleanup. A late model result cannot recreate erased
support. The response reports `affect_cleanup`; `pending` means the physical
cleanup must be retried, not that erasure is done.

Affect event responses expose `source_lineage` and `evidence_basis`. Events
with unknown or explicit independent origins stay labeled
`unlinked_observation`. This is source-aware retention, not proof that an
inferred state is correct. Raw source retention, learned assertions and
inferred relationship state remain distinct, and the attribution and
authority rules above still govern which conversations can update these
stores.
