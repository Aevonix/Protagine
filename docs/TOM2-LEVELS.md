# Leveled Cross-Contact Theory of Mind (ToM2)

Protagine keeps second-order inferences — *who appears to know / not know
which shared fact* — in a refs-not-content store (`tom/tom2.py`). This
document describes the **leveled rendering system** that decides, per
conversation and per reader, **every turn**, how much of that model may
surface, and how to turn all of it off with one variable.

Everything below ships **default-inert**: with a stock configuration the
assembled context is byte-identical to a build without this system (test:
`tests/test_tom2_wiring.py::test_defaults_byte_identical_to_neutralized_block`).

Automatic ToM2 sections require current canonical source links for the fact
and every supporting reference. A missing fact does not become an anonymous
"shared fact" assertion. Source corrections also exclude the old knowledge
inference, including when the correction has subsequently been erased. The
compact ToM2 renderers cannot carry a complete correction packet; ordinary
memory recall still supplies attributed corrections with their sources.
Selection is rechecked after other context producers finish. Explicit fact
and inference APIs retain their history, and source support alone does not
establish that a knowledge inference is semantically correct.

## Level semantics

| Level | Audience | What renders |
|---|---|---|
| **0** | owner only | Today's behavior: the owner-facing asymmetry section (`PROTAGINE_TOM2_CONTEXT`) and owner API surfaces. Nothing about the model reaches any other reader. |
| **1** | the reader, about themself | Self-reflexive prior: the reader's own `knows` rows (fact text they already own) plus ONE content-free caution line when `unaware_of` rows exist about them. No third party is ever mentioned. |
| **2** | the reader, about third parties | Epistemic topology ("X has not heard: …") through the full eligibility pipeline. By construction (the H3.5 double gate) every rendered fact text is a fact the reader **already owns** — level 2 can add topology, never new content. |

## The effective level (a min-chain of independent brakes)

```
effective = min( PROTAGINE_TOM2_LEVEL          (default 0),
                 PROTAGINE_TOM2_MAX_LEVEL      (default 1),
                 cap(environment risk)      (PROTAGINE_TOM2_RISK_CAPS, default 0:2,1:2,2:1,3:0),
                 2 if live-enforce-evidence else 1,
                 2 if PROTAGINE_TOM2_CROSS_CONTEXT else 1 )
       … and 0 on ANY error anywhere.
```

Environment risk (`tom/env_risk.py`) grades each (conversation, reader)
pair R0–R3, **monotone and fail-closed**: a lower grade needs positive,
verified evidence (declared-private gateway, strong identity resolution,
known census, tier floors); every missing signal — and every error — is
R3. `PROTAGINE_ENV_RISK_GATEWAY_CLASS` must explicitly bless a gateway as
`private` before anything can grade below R3; Protagine ships no gateway
names.

Enforce evidence must prove that a transport-owned egress mediator actually
withheld or emitted the exact digest-bound output on this gateway. No
receipt-backed egress mediator is wired in the current build, so the evidence
term remains 1 and Tom2 level 2 remains capped at level 1.

## Forced-downgrade list (no human in the loop)

Any one of these drops the level THAT TURN, silently:

- unknown / shadow / weakly-resolved participant sighted → level 0
- group room, or any second non-owner in the window → level ≤ 1
  (any unresolved member → 0)
- public / embodied / unclassified gateway → level 0
- the **subject** of an inference sighted in the conversation → that
  inference is excluded (never model someone into the room they are in)
- the subject is the owner → excluded (owner ignorance is never narrated)
- no enforce evidence, breaker tripped, or check de-allowlisted → ≤ 1
- machine/system turn, unresolved reader → 0
- resolver / classifier / store error of any kind → 0
- exposure budgets exhausted (`PROTAGINE_TOM2_BUDGET_*`) → the row does not
  render
- owner pair-approval missing / expired / revoked → the row does not
  render

The resolution is cached ≤ 60s per (conversation, reader); a decayed
brake takes effect within a minute, everywhere at once.

## Graduation ladder

1. **Ship dark** (all defaults). Observe presence, `/env-risk`, and
   `/tom2/status` for a while; confirm your private DM surfaces grade
   R0/R1 and the doctor is clean.
2. `PROTAGINE_TOM2_LEVEL=1` — self-reflexive priors only.
3. Build and verify a transport-owned egress mediator that persists receipts
   binding the policy, evaluated candidate digest, decision, and exact applied
   output digest. Only that receipt-backed mediator may supply the resolver's
   enforce-evidence probe.
4. `PROTAGINE_TOM2_MAX_LEVEL=2` + `PROTAGINE_TOM2_CROSS_CONTEXT=1` +
   `PROTAGINE_TOM2_LEVEL=2`, with `PROTAGINE_TOM2_L2_APPROVAL=required`
   (default) and per-pair approvals via `POST /v1/host/tom2/approvals`.

The current system cannot complete step 3 and therefore cannot run level 2:
no receipt-backed applied-output evidence means the min-chain caps at level 1
— by construction, not by policy or audit-row volume.

## Kill switch / panic

**`PROTAGINE_TOM2_LEVEL=0`** is the single-variable kill: the context wiring
is skipped entirely (level 1 AND 2, every conversation) on the next turn.
Nothing else needs to change. Verify with `GET /v1/host/tom2/status`
(`configured: 0`) and `protagine doctor` (`tom2-level-coherence` reports the
kill switch).

For a full stand-down beyond rendering: revoke pairs
(`POST /v1/host/tom2/approvals` with `action=revoke`) and set
`PROTAGINE_TOM2_CONTEXT=0` to drop the owner section too.

## Reversibility

- **What was disclosed:** `GET /v1/host/tom2/exposure` — every level-2
  rendering by reader/subject/fact-ref/conversation (refs only, never
  fact text), plus live budget posture.
- **Un-model a fact:** `DELETE /v1/host/mind/facts/{id}` cascades via
  `Tom2Store.delete_for_fact` — the fact's inferences are dropped, and a
  dangling ref could never render anyway (H3.5 fails closed).
- **Un-approve a pair:** `POST /v1/host/tom2/approvals` `action=revoke`
  (approvals also expire on their own, `PROTAGINE_TOM2_APPROVAL_TTL_DAYS`,
  default 30).

## Observability

- `GET /v1/host/tom2/status` — mode, counts, `{configured, max,
  risk_caps, sample_decision}` (every brake term of a live resolution).
- `GET /v1/host/env-risk?conversation_key=…&contact_id=…` — grade + census.
- `GET /v1/host/tom2/exposure`, `GET /v1/host/tom2/approvals`.
- `protagine doctor` — `tom2-cross-context`, `tom2-risk-caps`,
  `tom2-level-coherence`.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `PROTAGINE_TOM2_LEVEL` | `0` | Requested level; `0` is the kill switch. |
| `PROTAGINE_TOM2_MAX_LEVEL` | `1` | Hard ceiling. |
| `PROTAGINE_TOM2_RISK_CAPS` | `0:2,1:2,2:1,3:0` | Per-risk level caps; malformed fails closed to all-0. |
| `PROTAGINE_TOM2_CROSS_CONTEXT` | `0` | H3.5 render gate (half of the level-2 requirement). |
| `PROTAGINE_TOM2_L2_APPROVAL` | `required` | Owner pair-approval requirement. |
| `PROTAGINE_TOM2_BUDGET_PAIR_DAY` / `_READER_DAY` / `_GLOBAL_DAY` | `1` / `3` / `10` | Exposure budgets per rolling 24h. |
| `PROTAGINE_TOM2_MUTUAL_WINDOW_DAYS` | `30` | Mutual-knowledge co-sighting window. |
| `PROTAGINE_ENV_RISK_GATEWAY_CLASS` | *(empty)* | `gateway:private\|public\|embodied` pairs; unclassified = hostile. |
| `PROTAGINE_ENV_RISK_WINDOW_HOURS` | `48` | Census / subject-presence window. |
| `PROTAGINE_CONV_PRESENCE` | `on` | Passive conversation census recording. |

## Residual risk (accepted, not hidden)

Level 2 discloses *that* the system models people, to trusted readers, at
a budgeted rate. Irreducibles: implication leaks (behavior divergence is
observable), aggregation over time (slowed by budgets, not stopped),
elicitation (a voiced prior is one paraphrase away). Structurally impossible
through the renderer:
new fact content crossing contexts, owner-ignorance narration,
co-present-subject surfacing, rendering to unknown / group / public
audiences.
