"""The mind: drives, concerns, feelings, deliberation, goals, authority, the tick, the ranker, outcomes,
the outbox and the audit log.

Architecture sections 3, 4.5 and 7. The mind makes only tool-less model
calls through its own router (at most one per tick); every effect is a
Hermes kanban task (assignee ``protagine-act``, idempotency key
``mind:<id>``) or a message the plugin sends verbatim. The initiatives
table is the intention store and the only audit log; ``mind.db`` holds the
concerns and the mind state.
"""

from .affect import Affect, AffectView
from .authority import Authority, Policy, Verdict, classify, decide_table, floor_class, may_contact_of
from .concerns import Concern, Concerns, MindState
from .deliberate import Deliberation
from .drives import DRIVES, DriveInputs
from .goals import Goals
from .rank import Candidate, eligible, pick, score
from .tick import Mind

__all__ = [
    "Affect", "AffectView", "Authority", "Candidate", "Concern", "Concerns", "DRIVES", "Deliberation", "DriveInputs",
    "Goals", "Mind", "MindState", "Policy", "Verdict", "classify", "decide_table", "eligible", "floor_class",
    "may_contact_of", "pick", "score",
]
