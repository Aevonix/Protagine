"""The mind: authority, the tick, the ranker, outcomes, the outbox and the audit log.

Architecture sections 3 and 7. The mind makes no tool calls; every effect is a
Hermes kanban task (assignee ``protagine-act``, idempotency key ``mind:<id>``)
or a message the plugin sends verbatim. The initiatives table is the intention
store and the only audit log.
"""

from .authority import Authority, Policy, Verdict, classify, decide_table, floor_class, may_contact_of
from .rank import Candidate, eligible, pick, score
from .tick import Mind

__all__ = [
    "Authority", "Candidate", "Mind", "Policy", "Verdict", "classify", "decide_table", "eligible",
    "floor_class", "may_contact_of", "pick", "score",
]
