# Hybrid recall and calibrated abstention

Automatic context and explicit memory search combine lexical source search in
SQLite with the optional Lance semantic projection. Both use the same canonical
source IDs and enforce participant/session scope before selection. New source
text and corrections remain available while semantic projection is pending.

An absent, incompatible or unavailable semantic index leaves lexical search
available and reports the semantic limitation. A working empty search is distinct
from an unavailable canonical store. There is no other memory store: the Neo4j
graph memory was removed in M8. See [source semantic recall](SOURCE-SEMANTIC-RECALL.md)
for projection identity, model swaps and the shared HTTP contract.

## Returning no useful memory

`PROTAGINE_RECALL_RERANK_MIN_SCORE` is optional and has no global default. Scores
have model-specific meanings. When a cutoff is configured and calibrated, the
reranker also evaluates candidate sets smaller than the requested result count.
Passages below the cutoff are omitted instead of padding the context. Omitted
scores cannot satisfy a configured cutoff. A reranker error retains the existing
fallback behavior; an empty result must not be interpreted as evidence that an
unavailable backend searched successfully.

The cutoff applies only when `PROTAGINE_RECALL_RERANK_CALIBRATION` matches the
SHA-256 configuration fingerprint supplied by the active reranker registration.
The fingerprint uses the provider, model, endpoint, prompt format, candidate
input format, optional weight revision, embedding configuration and optional index generation. Changing
those values invalidates the cutoff. A custom rerank function supplies current
metadata through `RecallSelector(rerank_fn, calibration_metadata=...)`.

An unmatched or absent fingerprint disables the cutoff and marks the returned
rows `mismatch` or `unverified`, with a warning. A matching configuration whose
weight revision is unavailable is explicitly marked
`configuration_verified_weights_unverified`. A configuration stamp cannot detect
an unannounced weight replacement behind the same endpoint/model alias. Operators
must invalidate calibration on such a replacement; setting a model name is not
proof of immutable weights. `PROTAGINE_RERANKER_REVISION` and
`PROTAGINE_RECALL_INDEX_GENERATION` accept known revisions when available.

There is no universal cosine or reranker threshold. A deployment should freeze
representative positive, paraphrased, corrected and no-answer queries, select a
cutoff on its development subset, then test its held-out subset once. Keep that
deployment's calibration values outside generic defaults.

The calibrated candidate format is
`grounded-quotation-bundles-v2-corrections-first`. Attributed corrections precede
the original evidence in ranking text; all members remain in the complete output
packet. Changing that format changes the configuration fingerprint even when the
provider and numerical cutoff stay the same. Before promotion, test the intended
cutoff against useful corrections, other useful evidence and genuine no-memory
queries, preserving any earlier failures. A configuration match alone is not
quality qualification, and irrelevant competitors inside useful queries do not
replace whole-query abstention controls.

Select the qualified code and its matching calibration metadata together.
Keeping an older stamp invokes the existing mismatch fallback, which disables
the cutoff; that is not a successful calibrated upgrade. Recovery must likewise
pair the previous representation with its previous stamp while retaining current
source and erasure state. No threshold fitting occurs automatically.

## Qualification

Required observed cases are: a new source found before its vector is available;
the old superseded source excluded; an irrelevant query producing no recalled
passages under a matching calibration; source handles visible in the assembled
context; scoped misses staying scoped; and a changed reranker configuration
invalidating its old cutoff. Existing consent and tool authority remain with
their execution owners.

An earlier development benchmark used actual LAN embedding and reranker models, a
disposable Lance store and a fixture-backed stand-in for the graph memory that has
since been removed. The frozen comparison in `benchmarks/source_recall` now drives
the production `collect_sources` and `select_memory` path directly (M8); its
`reference-results.json` still records the earlier three-arm run, whose arms tied,
until the next measured run replaces it (see that README).

The initial implementation replay retrieved the expected sources but scored
15 of 24 complete behavior cases against its original fixture. Review then found
that the fixture conflated a request for today's footage with a window beginning
the previous day. That aggregate is not a reliable acceptance score. Explicit
calendar, validity and contradiction cases are required. A reranker cutoff does
not replace occurrence-time filtering or canonical contradiction state. This
patch preserves recorded timestamps and existing contradiction counts; it does
not implement those remaining memory semantics.

## One selection path for turn context

`/context/assemble` combines scoped lexical and semantic source hits,
source-claim expansions, media descriptions and relevant contact estimates.
Canonical source search independently enforces contact/session scope and
source-message erasure. A partially redacted turn can retain unrelated quotations.

Canonical candidates feed one rank-fusion and reranking pass. The calibrated
cutoff can reject every candidate. Context contains at most five total records
in one `protagine-memory` section. There is no separate conversation-evidence
injection. Quotations remain evidence of what was said, while claims and contact
estimates retain their distinct interpretation and uncertainty.

Combined recall submits at most four times the requested packet count to the
inline reranker, in fused rank order: 20 candidates for the default five-record
packet. This bounds model work as producers expand, while leaving room to select
useful passages below the first five. The existing timeout still applies. On
successful active reranking, only that submitted set competes for the packet;
unsubmitted rank-fusion scores are not compared with cross-encoder scores.
Disabled, shadow and failed reranking preserve the full original candidate set.
This can exclude relevant evidence below the submission bound. Evaluate source
retention alongside latency on the deployment's corpus; a smaller batch does
not establish a relevance cutoff or solve unrelated-memory injection.

Plain attributed quotations that exactly repeat the current request are
supplementary: selection places them after independent evidence before bounded
reranking and final packing. Original bytes and source records are retained;
qualified assertion histories, annotations and uncertain attribution are exempt.
This reduces displacement by repeated questions, including while a correction
awaits claim processing. It does not guarantee primary-source coverage when
many distinct assistant retellings compete, or recover sources missed during
candidate retrieval. The five-record limit and character budget are unchanged.

The current session's own quotations and conversation pairs stay candidates:
whether the host still shows them verbatim is not known across its agent
rebuilds and compactions, and leaving them out on a wrong guess loses the only
verbatim evidence for turns the host has summarised away.

The default combined rendered budget is 4,000 characters, adjustable through
`PROTAGINE_RECALL_CONTEXT_MAX_CHARS` up to 24,000; zero suppresses this packet.
This is a character limit, not an asserted token count. Shortened excerpts carry
`excerpt_truncated=true`, and source bytes remain intact in the source store.
Records retain `kind=belief` or `kind=source_quote`, source/turn handles, speaker
role, and occurrence/ingestion times when available. Quoted content is explicitly
evidence rather than instructions or an accepted belief.

Reranker failure uses one shared bounded fallback and marks returned records
`rerank_status=unavailable`; it does not promise calibrated abstention during an
outage. Unconfigured reranking retains rank-fusion fallback. This budget
covers the mixed memory packet, not other existing context sections.

## Per-turn context selection

The memory packet is one lane of the per-turn context. With selection on
(`PROTAGINE_CONTEXT_SELECTION`, default `on`), `/context/assemble` pools the
items of every lane it assembled (recalled evidence, pending commitments,
recorded views, lessons, the relationship and communication-landscape lines,
observed work and the other sections), has the same reranker score each against
the incoming message, and keeps the best-scoring items within one character
budget (`PROTAGINE_CONTEXT_SELECTION_CHARS`, default 3,000). Kept items stay in
their sections, in their assembled order and words; a section keeps its header
while any of its items remains, and its citations shrink to the sources its
remaining text names. An assertion card's shared quotation (`evidence_ref`)
follows the card that cites it. The judge reads each item's words with its
section title, without identifiers, hashes or timestamps, at most 600
characters of it; at most `PROTAGINE_CONTEXT_SELECTION_CANDIDATES` (48) items
are judged. Above that cap every candidate is first ranked against the message
by the words it shares with it (rarer words weigh more, a five-letter stem
absorbs inflections), lane priority breaking ties, so the cap never drops the
item the message asks about in favour of a higher-priority lane.

Pinned items are never judged and never dropped: Current Time, Expected replies
(the open asks), the owner's communication preferences and priority
corrections, the corrections to earlier context below, and every commitment
that is overdue or due within `PROTAGINE_CONTEXT_DUE_SOON_HOURS` (24). A context
that already fits is returned without a reranker call. With no reranker
configured, or when it fails, answers late
(`PROTAGINE_CONTEXT_SELECTION_TIMEOUT_MS`, default 2,000) or returns an
incomplete, duplicate or non-finite score set, the assembled context is used
unchanged and the fallback is logged (a failure at most once per five minutes
at warning level). `PROTAGINE_CONTEXT_SELECTION_MIN_SCORE` (default unset)
drops judged items below a score even when the budget has room; like the recall
cutoff, a threshold belongs to the deployment's reranker and corpus.

Measured on the memory family's development render (two runs, three plugin
arms, 433 turns, 144 probe turns) with Qwen3-Reranker-8B and the Qwen3
template: at 3,000 characters every probe answer present in the assembled
context stayed in the selected one (90 of 90) and the mean block went from
1,368 to 1,269 characters (most blocks already fit). With each probe block
padded to a live-sized one (about 11,600 characters, from other families'
sections), the mean went to 3,194 characters and 90 of 90 answers stayed; at
2,000 characters 87 of 90 stayed. This is answer retention in the context, not
a model-in-the-loop accuracy run.

## Superseded values

A line of any section that states a value the record has since superseded
keeps its words and gains the current value: `[superseded: now "<value>" since
<date>]`, `corrected to` for a correction, `rescheduled to` for a commitment
deadline. The records are the scope's changed and corrected source claims
(followed to the end of the chain, however long, and only to a claim read and
found superseded by nothing; a cycle, a chain past 4,096 claims, or an erased or
unattributed successor asserts nothing) and the listed commitments' previous deadlines. A line is a record's
only by identity: it carries the commitment's `id=`, or the claim's id or the
`turn:` of the source that stated it. Shared subject words, or the same value on
another record's line, never count. Values are matched in the line's words (a
JSON record read as its string values, identifiers and timestamps left out, and
every JSON escape read as the character it stands for, so `Caf\u00e9` is
`Café`), so
a value of any length counts on its own record's line and a "42" inside a
timestamp does not. A record's line counts when it carries the
old value without the current one, or a note whose value the record has since
replaced (a deadline moved twice), or, on a commitment line, a due other than
the current one.

The host replays earlier turns' context as it was. The sidecar keeps, per
conversation and in memory only, what it served; when a value served earlier has
been superseded since, the new turn carries a pinned "Corrections to earlier
context" section naming the record, the old value and the current one. A
correction is owed while the last line of that record the conversation was
served showed a replaced value; once a correction (or a line showing the
current value) has been served after it, it is delivered and not repeated. At
most eight are listed per turn, the most recently served stale values first;
the rest follow in the next turns. That record is
bounded, per process and lost on restart, so after a restart only the current
turn's lines are annotated.

Integrated regressions cover mixed abstention, one budget/section, selection
without any graph import, visibility before model input, failure fallback,
excerpt preservation and the shared result limit.
Six neutral checks with the current LAN reranker also passed, including three
no-answer queries; these are integration checks, not a fresh calibration fit or
a representative holdout benchmark. A deployment must still qualify quotations
and its actual corpus before treating its cutoff as measured for that corpus.
