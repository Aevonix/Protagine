# Source references for native review proposals

Protagine connects actual tool-result references to Hermes background review
and skill evaluation. Reviews and adoption remain native-owned.

The native request middleware captures references to structured tool failures
linked to actual assistant tool-call IDs. It uses the same participant scope
and inherited ContextVar mechanism as the existing background-review boundary.
User prose containing the word `error`, successful file contents and unlinked
tool results do not qualify. Guest, cron and subagent contexts are excluded.

The existing native pending skill payload retains at most sixteen references:
session/turn, tool call ID, tool name, failure classification and
`request_visible_result_sha256`. These hashes identify the result string in the
model request. They are not claims about canonical SessionDB bytes, an original
document, tool arguments or content before redaction/serialization. No raw
result or argument is copied into this metadata. Missing evidence is `null`.

Session and turn identify the request observing the result. Its history may
contain an earlier failure that has already recovered, so these references do
not establish the original failure turn, current failure or recurrence. Chat
Completions and Responses support is limited to JSON string tool outputs;
list-valued multimodal outputs are not captured by this bridge.

Model-provided evidence fields are replaced with the captured references.
The existing evaluator carries those references into its native evaluation
ledger, including non-improvements. Its comparison, apply and rollback rules
remain unchanged. A link to a failure is evidence for investigating a proposal;
it does not prove the proposal fixes that failure or was used afterward.

The existing foreground `capture_review_parent` request middleware calls the
capture function after participant resolution:

```python
from .review_evidence import capture
capture(scope, kwargs.get('request'),
        durable=(config.get('native_reviews') or {}).get('enabled') is True)
```

Its existing early return for background review must remain: Hermes inherits
the foreground references into the review fork. This also preserves references
when native routing reduces the review's input to a conversation digest.

An actual native post-turn review fixture executes a failed neutral `read_file`,
then verifies that Hermes' real review fork stages the matching call/result hash
under the inherited owner's session and turn. Successful reads retain no failure
evidence; the guest control cannot stage an owner skill change.

Focused packaged tests also cover linked Chat Completions and Responses results,
missing authority/evidence, user-content rejection, independent review-context
snapshots, duplicate result references, model-provenance replacement, and the
unchanged native proposal/evaluation/recovery path. Controlled oracle outcomes
qualify those contracts, not a measured model improvement.

The motivating operational class is unsupported regular-expression features
in native file search. A stock native file/terminal experiment reproduced
lookahead, lookbehind and backreference failures; explicit `rg --pcre2` recovered
the intended matches, while simple and no-match controls already worked.
That establishes a supported recovery option. It does not establish learned
adoption, task impossibility without a correction, or an automatic skill gain.
No tool implementation or regular-expression meaning is changed by this work.

## Recurring failures across conversations

With native reviews enabled, the same middleware appends ordinary skill-use
failures to Hermes' existing skill ledger. This path only accepts a resolved
owner, a successful `skill_view`, and a linked structured error after the
current user message. It records the exact viewed skill content hash. Earlier
errors replayed in a later turn, CLI/system work and background reviews do not
become ordinary experience. Original task text and error prose are not copied.

`review_experience.next_batch` selects the same failure from at least two
distinct ordinary turns, possibly from different sessions. Unknown error
classes must also match the exact result hash. One result does not count twice
when its history is replayed. Review receipts consume selected observation IDs;
new experience is required for another batch. A consumer must still check
current skill ownership and use its existing evaluator before any adoption.

Viewing a skill before an error establishes association, not causation. These
observations do not detect unsupported prose or every task failure. A caller
that deliberately injects an operator exercise into an authenticated owner
transport must preserve its explicit test provenance or suppress collection;
the participant fields alone cannot distinguish it from ordinary owner work.

## Continuing from a failed proposal

The existing ordinary-review job can assess a changed proposal after a recorded
validation failure or measured non-improvement. The native ledger retains the
exact failed proposal and its diagnostic. A later job firing can claim a linked
successor, with at most two successors per original review. The original
observations remain consumed and do not become new experience.

The reviewer receives the failed proposal and its public diagnostic. Hidden
evaluation answers are withheld. Current sources, owner and job must still
match. An identical candidate, repeated validation failure, unsupported change
or owner rejection stops the chain. Rejection is checked again during staging
and evaluation, including when it happens while the reviewer is working.

Every changed proposal still requires independent evaluation before activation.
Only successful activation settles its exact superseded pending proposals;
their failure history stays in the ledger. An unavailable assessment or oracle
does not justify rewriting a skill or resetting a consumed evaluation. Explicit
evaluation remains available after an operator verifies that recovery is valid.
These transitions do not by themselves establish a useful improvement.
