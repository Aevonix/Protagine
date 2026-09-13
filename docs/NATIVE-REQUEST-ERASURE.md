# Source forgetting on native Hermes requests

The general adapter uses Hermes `llm_request` middleware to reconcile canonical
source erasure before each normal model request, including resumed sessions and
tool-loop retries. The built-in context compressor remains selected. There is no
custom compressor, model call, background sweep, or new deletion store.

The context response carries the contact's canonical erasure watermark captured
before its producers run. The memory provider stamps its returned packet with
that contact and watermark; source IDs and quotation provenance remain in the
existing evidence body. Hermes wraps and persists this packet as `api_content`.
At request time the adapter consumes the existing erasure feed into the same
durable rules that protect the turn outbox.

The existing pre-turn observation also supplies Hermes' clean content alongside
persisted `api_content`. An ephemeral map for that active turn preserves this
exact association when clock notes or other native context surround the source.
It is cleared on completion and bounded across concurrent turns. Multimodal
messages are matched as complete original block lists after removing appended
packet-only notes, so forgotten image bytes do not survive as residual blocks.
The current direct input is new evidence: an owner can deliberately tell the
agent the same fact again. This exception requires the actual current input
from the authenticated native turn observation and its matching request row;
it never selects an old historical row merely because it is last. Recalled
packets on that new input still undergo freshness checks. Existing canonical
source and outbox tombstones remain unchanged.

- Exact whole-message matches against deleted source hashes become an explicit
  forgotten-source placeholder. Original session and speaker hashes also identify
  exact full-message copies carried into another session for the same contact.
- Retained packets with an older or unknown watermark are removed after erasure.
  This invalidates derived relationship and commitment sections together with
  quotations. Fresh assembled packets remain eligible. Untagged historical
  memory fences are retained only while the contact has no canonical erasures.
- A forget during assembly invalidates that packet on the next freshness check.
  The feed check is the request's observation point; a forget committed after the
  check can only affect the next request. No claim is made to recall an in-flight
  provider request.
- Feed reconciliation has a 250 ms cooperative deadline and at most four pages.
  If it is unavailable or incomplete, earlier conversation history and recalled
  packets are withheld. The current turn and its tool results remain available,
  so an endpoint outage does not replay or silently erase completed build steps.
  Already retained deletion hashes still apply. The middleware returns a reduced
  request instead of raising into Hermes' fail-open middleware behavior.

The request filter closes an exact replay boundary; it does not rewrite stored
transcripts. The separate native writer integration below removes owned copies.
Arbitrary assistant
paraphrases, summaries without retained packet markers, tools containing copied
arguments, static identity files and unlinked historical notes have no invented
lineage. Memory from another contact requires that contact's erasure scope; a
relationship to that contact is not itself source ownership.

## Native owned-copy reconciliation

The qualified Hermes build supplies a selective native writer and two settled
turn hooks. Authentic source reads and actually supplied recall record their
source revisions, native database location and current input anchor in one
metadata-only table in the existing outbox. This also works when ordinary CLI
capture is disabled. It does not create a canonical copy of the CLI answer.
Before each request, the qualified host supplies the exact persisted row at its
validated current-turn index. This refreshes storage ownership after rotation
or in-place compaction without selecting the last user message or matching text
against another turn. Old and newly authenticated rows retain separate ownership.
Canonical admission stays separate from native storage: a compressed row may
contain an API task wrapper. Its native content hash validates the stored row;
the admitted canonical message hash selects partial erasure. An unavailable or
changed host anchor withholds affected source context rather than reusing a stale ID.
Ordinary canonical capture records its native database location and verified
current user/final assistant row IDs before enqueue. A retained tool observation
binds the exact completed native result row before publication. Identical text
in a later turn is not another copy of that origin. Historical sources without
row bindings require unique matches; ambiguous or unobserved origins remain
pending. Partial erasures select only the affected bound message, preserving
the other speaker's input. Reconciliation reads actionable ownership and looks
up required origins by source ID, rather than decoding all past origins each turn.

Optional tool-input retention also binds its sole original assistant call row
with a native payload digest. That row is erased individually, keeping native
call identity but removing its arguments. A row shared with other tool calls
cannot be retained this way; the tool returns an error and can retain the result
without input instead. This optional capability requires the native snapshot API.

Canonical erasure events and pending native ownership commit with the existing
feed cursor. Reconciliation verifies each anchor, selects its owned turn span
and derives native payload preimages in one SQLite read transaction. The native
writer checks those preimages, writer leases and that snapshot's latest message
ID inside its mutation transaction. An answer appended after selection leaves
cleanup pending for a fresh selection, instead of stranding that late copy.
Native row IDs, routing and tool-call pairing remain intact. Owned tool results,
reasoning and assistant answers lose their payloads and FTS entries. A recalled
copy attached to independent human input clears only that row's `api_content`.
This is conservative turn dependency, not proof that every answer word used the
forgotten source.

An active writer leaves the operation pending. The standalone settled hook or
the gateway's post-release hook retries after persistence; the existing gateway
idle tick can retry retained work. Each attempt expands the verified turn again,
including a final answer written after the first attempt. Gateway erasure also
evicts the affected agent cache. Changed anchors and unknown historical profile
locations stay pending with a retained reason. Previously retained erasures can
finish during a sidecar outage; discovery of newer events remains unconfirmed.
An incomplete feed page also remains pending, with its cursor retained for the
next existing callback. It does not report complete cleanup after one partial page.

Completed ordinary turns publish a canonical memory copy only after exact native
origin retention succeeds. Failure logs the skipped capture and preserves the
reply already persisted by Hermes; it does not create an unbound memory copy or
an empty origin record.

Failed ownership retention withholds the affected source-read result or recall
from the model request and preserves ordinary current input. The response or
middleware result reports the failure. This does not make a cross-store atomicity
claim: Hermes may already have persisted recalled `api_content` before request
middleware runs. If its ownership write then fails, withholding model exposure
does not prove that preexisting native copy was physically erased during the
storage outage.

The outbox schema advances to version 3 without copying source content. Every
process sharing that outbox must select the updated adapter before reopening it,
including separately pinned voice and helper clients. An older adapter cannot
reopen this schema. Recovery must retain the version 3 reader and the current-row
middleware interface, including the post-tool compression index correction, to
preserve recall through compression. If the selected Hermes build lacks the
native writer hooks, storage cleanup remains pending until that interface
returns; ordinary request filtering still applies.
Restoring an old database would restore erased data and is not a rollback path.

The new tests exercise real native SQLite/FTS, captured and capture-disabled
readers, preserved human input, late answers, replay, changed anchors, separate
profile databases and the actual gateway writer/cache boundary. They use
synthetic sources and controlled calls, not live model observations. Unknown
historical reads, untracked compaction summaries or forks, trajectory files,
request dumps and backups still need explicit ownership and cleanup. Do not
report global forgetting from completion of this bounded storage operation.

## Native history retrieval

The existing `tool_execution` middleware also reconciles `session_search`
after its ordinary native authority check and before its result reaches the
model or execution observer. This covers discovery, reading, scrolling and
browsing. It uses the selected native database read-only, resolves returned
message IDs to their full bytes, and checks the bounded originating turn's
user/assistant anchors. It does not dump whole sessions or rewrite history.
The same exact source-hash rule used by request replay excludes a forgotten
turn's assistant arguments and tool results, including a scroll window whose
user anchor is outside the returned excerpt. Unrelated turns remain readable.

Optional native message hashes on the existing scoped source-freshness API
resolve to canonical source IDs and versions. The current principal and viewing
session determine visibility; the native session selector supplies no grant.
Known deleted hashes remain resolvable as erased. Authentic returned evidence
uses the existing source-read receipt and supplied-source lineage, so a later
erasure withholds an already-opened result and invalidates canonical descendants.
Quoted marker text never registers a read receipt. Unknown native history has
an explicit untracked count and no fabricated canonical parents.

Native titles and browse previews have no exact source identity. They are
omitted from model-facing history results; session links, time, channel, counts
and traceable message excerpts remain available. Open a session to inspect its
evidence. Unavailable storage, incomplete erasure freshness or an unresolved
native turn returns an explicit failure rather than an empty successful result
or an unchecked fallback.

History filtering itself is logical non-recollection of known sources. The
native writer above separately erases selected owned SQLite/FTS payloads.
Unlinked history, communication summaries, archived prompts, logs,
backups and previously unlinked paraphrases remain outside this projection.
It does not revoke arbitrary owner shell/file access or make a claim about
bytes already sent to a model. Do not report complete forgetting until the
requested storage surfaces have actually been covered.

The native qualification builds the public wheel, loads it in the pinned Hermes
runtime, calls the real memory provider against canonical source recall, uses
Hermes' own injection and SQLite persistence, then resumes twice through its
built-in compressor and conversation loop. The first captured provider request
contains the neutral source. After the standard forget API, the second contains
neither that exact source nor its stale recall packet. Controlled inference is
used; the test explicitly confirms the native storage limitation remains.
