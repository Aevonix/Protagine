"""Source claims: the structured memory derived from the canonical source ledger.

Every claim cites the turn and message it came from (``source_claims``), and
``SourceClaimProjection`` is the one scoped reader that context assembly, the
preference view and the mind's consolidation use. Contradictions and duplicates
are found by the nightly consolidation over these rows; there is no separate
belief store.
"""

from protagine.beliefs.promotion import MEMORY_KINDS, PROMOTION_VERSION
from protagine.beliefs.source_projection import SourceClaimProjection
from protagine.beliefs.source_time import MemoryTimeQuery, filter_unstructured

__all__ = [
    "SourceClaimProjection",
    "MemoryTimeQuery",
    "filter_unstructured",
    "PROMOTION_VERSION",
    "MEMORY_KINDS",
]
