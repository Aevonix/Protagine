"""Specialized intelligence components.

Seven domain-specific components that provide tool learning, self-reflection,
task planning, session continuity, research orchestration, preference learning
and anomaly detection. Self-initiated work is the mind's (``protagine.mind``).

Public API:
    - ``ToolLearner`` / ``ToolUsage`` / ``ToolPreference`` — learn tool preferences
    - ``SelfReflector`` / ``Reflection`` — self-evaluation and improvement
    - ``TaskPlanner`` / ``TaskPlan`` / ``SubTask`` / ``TaskPriority`` — task decomposition
    - ``SessionContinuity`` / ``SessionContext`` — cross-session context
    - ``ResearchOrchestrator`` / ``ResearchReport`` / ``ResearchResult`` / ``ResearchSource`` / ``SourceType`` — multi-source research
    - ``PreferenceLearner`` / ``Preference`` — user preference extraction
    - ``AnomalyDetector`` / ``Anomaly`` / ``AnomalyType`` — unusual pattern detection
"""

from .tool_learner import ToolLearner, ToolPreference, ToolUsage
from .self_reflector import Reflection, SelfReflector
from .task_planner import SubTask, TaskPlan, TaskPlanner, TaskPriority
from .session_continuity import SessionContext, SessionContinuity
from .research_orchestrator import (
    ResearchOrchestrator,
    ResearchReport,
    ResearchResult,
    ResearchSource,
    SourceType,
)
from .preference_learner import Preference, PreferenceLearner
from .anomaly_detector import Anomaly, AnomalyDetector, AnomalyType

__all__ = [
    # Tool learning
    "ToolLearner",
    "ToolUsage",
    "ToolPreference",
    # Self-reflection
    "SelfReflector",
    "Reflection",
    # Task planning
    "TaskPlanner",
    "TaskPlan",
    "SubTask",
    "TaskPriority",
    # Session continuity
    "SessionContinuity",
    "SessionContext",
    # Research
    "ResearchOrchestrator",
    "ResearchReport",
    "ResearchResult",
    "ResearchSource",
    "SourceType",
    # Preferences
    "PreferenceLearner",
    "Preference",
    # Anomaly detection
    "AnomalyDetector",
    "Anomaly",
    "AnomalyType",
]
