# Frozen persistent-workflow evaluation

`paired-agent-workflows-1` adds twelve public, versioned workflows to the paired
Hermes versus Hermes + Protagine benchmark. It tests whether useful work survives
conversation boundaries, corrections and a restarted agent process. The existing
18-case and 60-case datasets remain unchanged.

This is an evaluation pack, not a secret holdout. Its prompts, source files,
checkpoints and graders are public. Freezing them prevents results from different
task definitions being presented as comparable; it does not make familiar tasks
unseen or prove production readiness.

## The twelve workflows

Each workflow has eight ordinary user turns, three sequential sessions, two
Hermes worker process restarts, and intermediate artifact snapshots after turns
3 and 6. Runtime indices are zero-based: restart before indices 3 and 6; snapshot
after indices 2 and 5. A post-restart session never resumes a pre-restart session
ID. Case-local files and the arm's persistent state survive within the workflow;
different arms, workflows and repetitions receive fresh isolated state.

| Family | Workflow | Useful outcome | Critical failure it detects |
|---|---|---|---|
| Recall | `restock-handoff` | Correct carton arithmetic and a reviewable procurement draft using the accepted supplier, delivery date and ceiling | An older, cheaper supplier or unrelated event count replaces the accepted arrangement |
| Recall | `current-restock-record` **control** | Procurement derived entirely from the current approved order | Older remembered procurement plans override current evidence |
| Recall | `proposal-does-not-replace-commitment` | Accepted review location and an unfinished slide obligation survive a proposed alternative | A tentative venue becomes a commitment, or an outline becomes a completed deck |
| Correction | `selective-delivery-correction` | Corrected destination, retained loading window and signature requirement, retracted token absent | A narrow correction erases unrelated requirements or resurfaces the retracted token |
| Correction | `venue-conflict-before-clarification` | An unresolved venue is visible before clarification, then resolved without changing other commitments | Silently choosing a venue and repairing it only after the answer arrives |
| Correction | `current-approved-schedule` **control** | Current approved schedule governs every field | Historical disagreement or an old obligation contaminates a complete current record |
| Recovery | `resume-stable-task-after-read-fault` | One task ID, preserved completed dependency, fresh checksum evidence and a ready local packet | Losing progress after restart, accepting failed evidence, duplicating work or inventing publication |
| Recovery | `due-followup-without-duplicate-send` | No early nudge; one stable unsent draft when the obligation becomes due | Early outreach, accepting an acknowledgment as completion, or duplicating the draft after restart |
| Recovery | `current-board-after-read-fault` **control** | Current task board is re-read after a transient fault and produces the correct handoff | Replacing current evidence with an old remembered task state |
| Scope | `same-name-owner-linked-contacts` | Correct person, project, format and deadline from explicit owner-confirmed aliases | Merging two people because their display names match or transferring one person's request |
| Scope | `public-brief-after-private-work` | Public draft keeps its agreed feature name and verified limitations while excluding private source fields | Leaking private context, inventing a release date or claiming publication |
| Scope | `current-directory-scoped-handoff` **control** | Current approved directory and request produce a scoped local handoff | An old contact mapping or deadline overrides the current record |

Eight workflows make earlier conversation useful. Four controls provide all
necessary current facts in an authoritative file. Each control shares a domain,
similar history, turn count and restart pattern with a related workflow. They
are not exact interventions with all other variables held constant: report the
control results separately, not as a causal estimate of memory overhead.

## Inputs and outcome checks

Each workspace starts with roughly 6–8 KiB of authored source material, including
dated project history, distinct contact records, proposals, current revisions
and plausible distractors. This is substantially more history than the older
small fixtures, but remains a bounded-history test, not context-window
saturation. All names, identifiers, email domains and private markers are
synthetic. No production conversations, credentials or personal data are used.

Both arms receive the same ordinary task prompts and output contracts. The pack
does not instruct either arm to use a particular memory API. File notes, native
session retrieval and the arm's available memory are legitimate ways to finish
the work. Loading a memory provider is not itself a success.

Every requested JSON deliverable has an explicit key and value contract.
Intermediate snapshots are taken when the turn finishes. The contradictory-room
case must have recorded uncertainty before the owner supplies the answer; the
follow-up case must have remained waiting before its deadline. A correct final
file cannot repair a failed earlier checkpoint. Source files are explicitly
read-only and their bytes must match the originals at both checkpoints and final
capture. A modification restored between snapshots is outside this check.

JSON key shape and substantive field values have separate assertions. Reports
can distinguish a correct answer with an extra key from a wrong supplier,
deadline or recipient. Overall workflow success still requires the declared
contract, every checkpoint, all native turns, and runtime requirements to pass.
Hand-authored negative examples cover plausible mistakes such as a stale due
date, lost dependency, early reminder, duplicate draft, private marker leak and
premature conflict resolution. They test the grader; they are not model results.

Two workflows inject a one-shot failure into the named native source read after
the first process restart. The prompt requests a fresh authoritative read before
cached information is used. Fault exposure must be reported separately from
getting the final answer right: a path that never consumes the injected fault
does not demonstrate recovery from it. This narrow native-read boundary is the
only fault exercised, not arbitrary network or storage failures.

A consumed fault must be followed by a successful native read receipt before
the next checkpoint. A file merely claiming the read succeeded does not pass.
When every functional and lifecycle check except fault exposure passes, the raw
result stays unchanged but the comparison marks recovery coverage unavailable.
A separate semantic or completion failure remains a failure.

## Running and interpreting the pack

Use the existing `protagine models paired plan` command with
`--dataset-version paired-agent-workflows-1`. For a release comparison, add
`--repetitions 3` before execution, then run the immutable plan through the
existing paired runner. The normal required provider, image, comparison-policy
and output arguments still apply. The image must advertise workflow restart
support. A plan does not call a model.

```sh
protagine models paired plan \
  --output ./workflow-release \
  --native-config ./candidate.yaml \
  --native-binding candidate \
  --comparison-policy ./comparison-policy.json \
  --container-image registry.example/agent-benchmark@sha256:IMAGE_DIGEST \
  --dataset-version paired-agent-workflows-1 \
  --repetitions 3
```

Replace the image placeholder with the new workflow-capable image's digest.
Use the same provider, comparison policy and image arguments for `paired run`,
with `--output ./workflow-release`; its dataset and repetitions come from the
frozen plan. See [the paired benchmark guide](PAIRED-AGENT-BENCHMARK.md) for the
provider and policy files, reporting and public export commands.

Three repetitions are 36 paired workflows, 72 isolated arm executions and 576
declared user turns. Do not select the best repetition or silently retry a failed
workflow into a success. The same case design appears three times, so those
repetitions are not 36 independent task designs. Use focused subsets while
developing a fix and the complete predeclared set at a release comparison.

The v1 budget is eight agent iterations per turn, up to 4,096 output tokens per
model call, five seconds of settling between turns and a 600-second workflow
deadline. Keep the context policy and serving configuration identical between
arms. These limits are part of the comparison, not settings to tune for a
particular model after seeing its failures. Report task completion, semantic and
schema checks, checkpoint failures, restart/fault receipts, wall time and full
foreground/background request accounting. Missing token usage remains missing;
it is not zero. Shared-endpoint contention can affect latency and must be
disclosed. The controls help expose added work that delivers no task benefit.

Do not merge these results into the 60-case percentage. Report the workflow pack
version and content hash, per-family results, controls versus memory-relevant
tasks, and all repetitions. A small win on twelve familiar designs is evidence
for those workflows, not a universal model ranking or proof of agent learning.

## Freeze and remaining gaps

`scenarios.json` is byte-hashed by `manifest.json`; the loader verifies both its
identity and declared structure. The prompts, source files, schedules, read
faults and oracle contracts are versioned together. Once results exist, a
substantive correction requires a new dataset version and fresh complete
comparison. Keep earlier results identifiable instead of rewriting the inputs
under their existing name. Runtime or grader fixes must also be recorded in
the run's implementation identity.

This pack does **not** establish simultaneous-session consistency, real channel
transport, autonomous scheduling, exactly-once message delivery, physical
erasure from every store, authenticated multiuser authorization, all-channel
confidentiality, multimodal memory, host recovery or long-term self-improvement.
The follow-up time is supplied in prompts. Public-scope checks inspect declared
artifacts, not every assistant message. Each restart co-restarts Hermes and the
fixture source worker; it does not
model an independently running production sidecar or a host reboot. Pending
and running source-job counts are recorded at shutdown. Interrupted projections
keep their ordinary leases, so immediate post-restart recollection may precede
completion. No benchmark-only flush or lease reset is performed. The excluded
capabilities require separate integration and operational evidence.
