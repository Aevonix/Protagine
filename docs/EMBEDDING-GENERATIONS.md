# Embedding generations

Canonical source messages and graph facts are retained independently of their
embeddings. A vector is a replaceable search projection. Equal vector dimensions
do not establish that two models produce compatible embeddings.

The runtime now binds its vector store to an immutable embedding identity:
requested model alias, reported serving model, declared weights or deployment
revision, dimension, document and query formats, normalization and quantization.
The requested alias and serving model are separate fields. Missing revision or
serving identity is recorded as `unknown`. `PROTAGINE_EMBED_REVISION` is an operator
declaration, not verified weights attestation. An endpoint that silently changes
weights behind the same alias without reporting a change cannot be detected
from that alias alone; declare a new revision and rebuild after such a change.
Endpoint addresses and credentials are not index identity and are not copied
into index metadata.

The pipeline retains its query instruction for its lifetime, and source semantic
recall uses that same query formatter. The API embedder checks response cardinality,
ordering, finite values and dimensions before returning vectors. A changed
reported serving model cannot silently reuse the old pipeline. This change does
not add dynamic embedding provider configuration; selecting a different
embedding provider still uses the existing deployment configuration path.
Index promotion itself takes effect in the running store without a restart.

For the `openai_api` text provider, `PROTAGINE_EMBED_DIMS` specifies the expected
output width; it does not request dimension reduction. To explicitly ask a
supporting endpoint for a width, set `PROTAGINE_EMBED_REQUEST_DIMS` to the same
positive integer (or set `EmbeddingConfig.request_dimensions`). With this option
unset, requests continue to omit `dimensions`. The client never truncates
vectors, and rejects responses with a different width. This option is not
supported by local or multimodal providers. A multimodal startup that selects it
falls back to text-only operation through the existing initialization path.
Changing the explicit request option changes the embedding identity even when
the expected output width is unchanged; rebuild before semantic use. Unchanged
configurations retain their existing generation identity.

Existing Lance collections have no trustworthy embedding identity. They remain
on disk as an unverified legacy generation, and semantic search through the
managed runtime refuses to compare them with a newly configured model. Canonical
lexical source recall remains available. The
installer's optional vector dependency remains optional.

The existing `protagine migrate-tier` and `/v1/host/memory/migrate` operation rebuild
all retained text collections into a separate Lance directory. The server
registers the same store semantic recall reads with that operation. Every
retained text collection is rebuilt from its retained text and metadata. A model-filtered partial rebuild cannot be promoted because
it would leave the replacement incomplete.

The generation catalog, active pointer and exact-ID deletion fences use the
existing source ledger, not another database service. Rebuilding is resumable:
already staged IDs, including new live writes, are retained. A failed batch
leaves the prior active pointer intact. A complete generation is promoted by
one SQLite transaction. Old generations remain available for inspection or a
separately qualified recovery. This packet provides no unattended old-generation
rollback command and does not restore authoritative memory or authority state.
The ordinary migration API rejects concurrent rebuilds in one server process.
The CLI stops on interrupted/resumable or unavailable status and explains how
to resume without claiming completion. `--wait-seconds` bounds waiting (600
seconds by default); reaching that limit does not cancel the server job.

All managed vector deletes create an exact-ID tombstone first and remove that
ID from active, staged and retained generations. Canonical-source erasure also
scans exact source links, including orphaned vectors whose graph row has already
disappeared. Recall and late writes check the same fences. The forget response
reports `vector_cleanup` independently; `pending` requires a retry and does not
mean that physical cleanup completed. `complete` means the rows are gone from
every generation's served view. Compacting the tables, which takes the erased
text out of the Lance data files and old versions, runs after the response
(`vector_purge: scheduled`): on a large store that was never compacted the
first pass takes minutes. A forget that arrives during a pass gets one more,
and a failed pass is retried by the next forget. Unlinked historical vectors cannot acquire
invented source ownership from matching text. Their source provenance remains
unknown.

Compaction is also routine, because every append writes a manifest that lists
every fragment of its table: a table nothing compacts grows its version history
quadratically (one upgraded store held 74.7 GB of manifests around 1.67 GB of
data). One background task per store, never a request, compacts one table at a
time and deletes its older versions:

- after a forget, every table (above);
- once per night crossed (the consolidation's boundary), scheduled by the mind's
  tick whether or not the mind is on: every table with an older version;
- every five minutes, a table holding `PROTAGINE_VECTOR_COMPACT_VERSIONS` (1000)
  versions or more.

A pass reads every retained manifest, about 130 MB/s whatever it prunes, so a
table whose manifests exceed `PROTAGINE_VECTOR_COMPACT_DAY_BYTES` (1 GiB) is
not compacted by the threshold: it waits for the nightly pass, and for at most
a day. Between two tables the task pauses as long as the previous one took (at
most 30 s). Each table's pass is logged at info with its reason, versions and
size before and after, and its duration; a failure is logged as a warning and
the pass goes on.

A table's pass holds the store's write lock from start to end, so writes to the
store (the source vector worker's, a forget's deletes) wait for it, while reads
go on. Tables created by earlier releases carry Lance's own auto-cleanup
(`lance.auto_cleanup.interval` 20 and `older_than` 14 days in the table's
config; the lancedb 0.39 Python API can neither read nor unset it), which runs
inside a commit: a commit beside a first pass once deleted the manifests that
pass was pruning, and the pass failed after 13 minutes. Under the lock no
commit, and so no auto-cleanup, runs beside a pass. A read opens the latest version and reads
its files as it goes, so a pass prunes nothing a read may still need: it waits
for the reads begun before it (at most `READ_DRAIN_SECONDS`, 300 s, then it
logs how many still run and prunes), compacts, prunes only what is older than
the version current when it began, waits for the reads begun before its
compaction, and then prunes all but the latest version. A version manifest that
vanishes under a prune anyway (a cleaner in another process) is retried once.

Rebuilding image embeddings requires a qualified model that can reproduce that
image embedding space. The text rebuild refuses retained image-vector rows
instead of replacing them with caption vectors and claiming compatibility.
Canonical retained originals and their fallible descriptions remain separate.
[Semantic source and caption candidates](SOURCE-SEMANTIC-RECALL.md) use their own
scoped text projection in the managed generation, followed by the existing
single context selector and budget. Caption retrieval quality still requires
measurement with the selected embedding and reranking models.

`test_embedding_generations.py` uses real LanceDB for identity mismatch, changed
dimensions, non-destructive failed update, failed rebuild/resume, new arrivals,
erasure, and the real migration/forget API. Controlled vectors test distinct
spaces; a separate private neutral qualification uses the actual LAN embedder.
That small qualification does not establish comparative retrieval quality or
the performance of re-embedding a production corpus.
