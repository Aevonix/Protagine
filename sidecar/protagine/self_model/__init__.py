"""Runtime outcome history, owner-directed perspective and existing trust helpers.

Execution records retain their source and correction history. Their prompt
brief describes recorded runtime labels and current load without establishing
task quality or current-model competence. Source-backed owner preferences and
dated attention are maintained separately from those outcome labels.

SelfModel is enabled by default (PROTAGINE_SELF_MODEL_ENABLED); individual outcome
producers must be wired and running to supply records. Authority (the floor and the
breaker) lives in the mind (``protagine.mind.authority``); there is no trust ladder.
"""

from protagine.self_model.store import CompetenceStore, SelfModel, self_model_enabled
from protagine.self_model.brief import self_brief
from protagine.self_model.journal import ActionJournal

__all__ = [
    "CompetenceStore", "SelfModel", "self_brief", "self_model_enabled",
    "ActionJournal",
]
